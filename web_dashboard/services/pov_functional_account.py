"""Minting a POV's Password Safe functional accounts from the guests' own credential.

Password Safe derives a managed system's **platform** from its functional account, so
onboarding a POV's VMs needs one per guest OS. Slice 6b took that as an input: two names
typed onto the Password Safe tenant, resolved to ids over REST. That is right for a POV
against a *customer's* tenant, where a Linux and a Windows functional account are the
first thing anyone builds. It is wrong for a BeyondTrust-owned POV tenant on day one,
where neither exists and the wire-up therefore skipped every VM with a reason that read
like a bug.

So a blank name now means **create one**, and this module is what creates it.

**The functional account is a real login, not a label.** It is the identity Password Safe
authenticates to the guest as in order to rotate the managed account. Which means it
cannot be invented: the credential put in it has to already work inside the POV's guests.

**Where that credential comes from, and why nothing is stored.** The lab platform already
holds it — Skytap's ``stored_credentials`` capability, the same source
``pov_resource_broker.platform_login`` reads for the Resource Broker install. It is read
live, per run, and never written to this database. Slice 5b made that decision for the
Windows install credential and the reasons carry over unchanged: a credential that is
fetched cannot go stale, and a POV whose template password changed picks up the new one on
the next run with nothing here to update.

**A functional account is per-POV, and that is the load-bearing choice.** One is shared by
every managed system that names it, so a tenant-wide account would have to hold a
credential valid on every guest of every POV in that tenant — which nothing guarantees.
Minting per POV narrows the claim to "valid on this POV's guests of this OS", and
:func:`pov_wide_credential` then *checks* that claim rather than assuming it: every guest
of that family must report the same login. Guests that disagree are a refusal naming how
many, because the alternative is an account that rotates the first VM and fails on the
rest — days later, on a schedule, which is the failure shape the Resource Broker's
``application_host_id`` exists to avoid and the same one this would reintroduce.

**Recorded so teardown is exact.** The minted id lands on the POV row the moment it
exists (``ps_linux_functional_account_id`` / ``ps_windows_functional_account_id``), for
the reason every other artifact id does: a re-derived id is how you delete the wrong
thing. Teardown removes exactly those two, and only after the managed systems that
reference them are off-boarded.

**An account this dashboard did not create is never deleted.** A name typed on the tenant
is the customer's account; it is resolved and left alone. Only the ids recorded here are
touched, which is what keeps "the dashboard cleaned up after itself" from meaning "the
dashboard deleted the functional account your whole tenant uses".

Nothing here logs or raises with a credential in it. The parsed *username* is safe to name
once parsing succeeded; the password never is, and neither is the raw stored text — see
``pov_credentials``, which is where that rule is written down and enforced.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovEnvironmentVM
from . import config_service, lab_platforms, pov_credentials, ps_api_service

logger = logging.getLogger(__name__)


class FunctionalAccountError(Exception):
    """A refusal an SE can act on. Travels into a job log and a VM's skip reason, so it
    names the POV's guests and the remedy — and never the credential it read."""


# The guest families a POV onboards, and the Password Safe platform each one's functional
# account is created on. These are the built-in platform *display names*; the minted
# account's platform is what then decides the managed system's platform, which is the
# whole reason the accounts are split by OS at all.
#
# Overridable by config key rather than hardcoded outright, because a Password Safe admin
# may rename a platform and there is no other way for this to find it. Not a Settings
# field: an install that needs it has a tenant-specific answer, and the far more likely
# fix is to type the existing account's name on the tenant and skip minting entirely.
_PLATFORM_DEFAULTS = {"linux": "Linux", "windows": "Windows"}

FAMILIES = ("linux", "windows")

# The column each family's minted id is recorded in. A mapping rather than two branches
# so a caller cannot read one family's column while writing the other's.
_ID_COLUMN = {
    "linux": "ps_linux_functional_account_id",
    "windows": "ps_windows_functional_account_id",
}

# What an SE does about a POV whose guests disagree. Carried as one sentence rather than
# appended per raise, so every refusal from this module ends the same way.
_REMEDY = ("Give the POV's guests of that OS the same login, or create one functional "
           "account by hand in Password Safe and name it on the tenant — a name there is "
           "used as-is and nothing is created.")


def platform_name(family: str) -> str:
    """The Password Safe platform a ``family``'s functional account is created on."""
    fam = (family or "").strip().lower()
    default = _PLATFORM_DEFAULTS.get(fam, "")
    return config_service.get(f"pov_ps_functional_account_platform_{fam}", default) or default


def display_name(env: PovEnvironment, family: str) -> str:
    """The minted account's display name — the field carrying tenant-side uniqueness.

    Password Safe's uniqueness tuple is (platform, domain, account name, display name),
    and the account name is whatever the guests' login happens to be — ``root`` on every
    POV in the tenant, most likely. So the POV's name is what distinguishes them, and it
    is also what makes a retry idempotent: the second create comes back a duplicate and
    ``create_functional_account_on_platform`` resolves it to the account already there
    instead of minting a second.
    """
    return f"pov-{env.name}-{(family or '').strip().lower()}"


def recorded_id(env: PovEnvironment, family: str) -> int:
    """The id of the account this dashboard minted for ``family``, or 0."""
    col = _ID_COLUMN.get((family or "").strip().lower())
    if not col:
        return 0
    try:
        return int(getattr(env, col, None) or 0)
    except (TypeError, ValueError):
        return 0


def _record_id(db: Session, env: PovEnvironment, family: str, account_id: int) -> None:
    col = _ID_COLUMN[(family or "").strip().lower()]
    setattr(env, col, int(account_id) if account_id else None)
    db.commit()


def guests_of(db: Session, env: PovEnvironment, family: str) -> list:
    """The POV's VM rows whose guest OS is ``family`` and which have a platform id.

    A VM with no ``platform_vm_id`` cannot be asked for its credential, and a blank
    ``os_family`` is deliberately not guessed at — slice 3's rule, and the reason a blank
    one is skipped rather than sorted into a family it may not belong to.
    """
    fam = (family or "").strip().lower()
    return [vm for vm in db.query(PovEnvironmentVM)
                            .filter(PovEnvironmentVM.environment_id == env.id).all()
            if (vm.os_family or "").strip().lower() == fam and (vm.platform_vm_id or "")]


async def pov_wide_credential(db: Session, env: PovEnvironment, family: str) -> tuple:
    """``(username, password)`` valid on **every** one of this POV's ``family`` guests.

    Read live from the lab platform and returned, never stored. Raises
    :class:`FunctionalAccountError` when the POV's guests do not agree, when the platform
    stores no credentials, or when none could be parsed — each with the remedy, and none
    with any part of what was read.

    Agreement is the point. A functional account is one credential used against every
    managed system that names it, so "they all report the same login" is precisely the
    precondition minting one requires, and the only place it can be checked.
    """
    fam = (family or "").strip().lower()
    caps = lab_platforms.capabilities(env.platform)
    if not caps.get("stored_credentials"):
        raise FunctionalAccountError(
            f"{caps.get('label', env.platform)} does not store VM credentials, so this "
            f"dashboard has nothing to build a {fam} functional account from. Create one "
            f"in Password Safe and name it on the tenant.")

    guests = guests_of(db, env, fam)
    if not guests:
        raise FunctionalAccountError(
            f"this POV has no {fam} guests with a platform id, so there is nothing to "
            f"read a {fam} login from.")

    mod = lab_platforms.adapter(env.platform)
    found: dict = {}
    problems: list = []
    for vm in guests:
        label = vm.name or "that VM"
        try:
            entries = await mod.stored_credentials(env.platform_environment_id,
                                                   vm.platform_vm_id)
        except Exception as exc:  # noqa: BLE001
            # Logged and not carried outward: the exception can name the appliance, the
            # resolved address, or a chained cause from somewhere unrelated. Same rule
            # pov_resource_broker.platform_login follows.
            logger.warning("POV %s: reading stored credentials for VM %s failed",
                           env.id, vm.platform_vm_id, exc_info=True)
            problems.append(f"{label} ({type(exc).__name__})")
            continue
        try:
            pair = pov_credentials.pick(entries, vm_label=label)
        except pov_credentials.CredentialParseError as exc:
            # `pick`'s message is already operator-facing and already quotes nothing.
            problems.append(str(exc))
            continue
        found.setdefault(pair, []).append(label)

    if not found:
        detail = "; ".join(problems) if problems else "no guest offered one"
        raise FunctionalAccountError(
            f"none of this POV's {len(guests)} {fam} guest(s) offered a usable stored "
            f"credential, so there is nothing to build a {fam} functional account from. "
            f"{detail}")

    if len(found) > 1:
        # Names how many distinct logins and which guests hold the odd ones out — never a
        # credential, and never which login "won", because none did.
        groups = sorted((sorted(names) for names in found.values()), key=len, reverse=True)
        shape = " vs ".join(", ".join(g) for g in groups)
        raise FunctionalAccountError(
            f"this POV's {fam} guests report {len(found)} different logins ({shape}), and "
            f"a functional account is ONE credential used against all of them — so "
            f"minting from any of them would rotate one guest and fail the others on a "
            f"schedule. {_REMEDY}")

    (pair, covered), = found.items()
    if problems:
        # A partial read is still usable — the agreement held across every guest that
        # answered — but it is recorded, because the guests that did not answer are the
        # ones whose rotation will fail later.
        logger.warning("POV %s: minting the %s functional account from %d of %d guests; "
                       "the rest could not be read: %s",
                       env.id, fam, len(covered), len(guests), "; ".join(problems))
    return pair


async def ensure(db: Session, env: PovEnvironment, family: str, *, api=None) -> dict:
    """This POV's ``family`` functional account, minted if it does not exist yet.

    Returns the same shape ``ps_api_service.get_functional_account`` does —
    ``{id, platform_id, platform_name, account_name}`` — because that is what
    ``pov_wireup.onboard_vm`` reads to set the managed system's platform, and a second
    shape here would be a second thing to keep in step.

    Three steps, in order, so the cheapest answer wins:

    1. An id already recorded on the POV row is resolved and returned. This is what makes
       a re-wire free, and what lets teardown resolve the account after the guests are
       powered off and their credentials can no longer be read.
    2. Otherwise the guests' credential is read and checked (:func:`pov_wide_credential`).
    3. The account is created, and its id recorded before anything else happens — an
       account that exists in a customer's tenant and is not written down here is one this
       dashboard can no longer clean up.
    """
    fam = (family or "").strip().lower()
    if fam not in FAMILIES:
        raise FunctionalAccountError(f"unknown guest family {family!r}")

    existing = recorded_id(env, fam)
    if existing:
        try:
            return await ps_api_service.get_functional_account(str(existing), api)
        except Exception as exc:  # noqa: BLE001
            # Deleted tenant-side, most likely. Clearing the id lets the next run mint a
            # fresh one rather than refusing forever over a row that is now fiction.
            logger.warning("POV %s: recorded %s functional account %s could not be "
                           "resolved, clearing it", env.id, fam, existing, exc_info=True)
            _record_id(db, env, fam, 0)
            raise FunctionalAccountError(
                f"this POV's {fam} functional account (id {existing}) is no longer in the "
                f"tenant ({type(exc).__name__}). It has been forgotten here, so the next "
                f"wire-up will create a new one.") from None

    pname = platform_name(fam)
    if not pname:
        raise FunctionalAccountError(
            f"no Password Safe platform is configured for {fam} guests, so a functional "
            f"account cannot be created for them.")
    try:
        platform_id = await ps_api_service.get_platform_id(pname, api)
    except Exception as exc:  # noqa: BLE001
        logger.warning("POV %s: resolving the %s platform %r failed", env.id, fam, pname,
                       exc_info=True)
        raise FunctionalAccountError(
            f"the Password Safe platform {pname!r} could not be resolved in this POV's "
            f"tenant ({type(exc).__name__}), so a {fam} functional account cannot be "
            f"created on it. Set pov_ps_functional_account_platform_{fam} to the "
            f"platform's name in that tenant, or create the account by hand and name it "
            f"on the tenant.") from None

    username, password = await pov_wide_credential(db, env, fam)
    label = display_name(env, fam)
    try:
        account_id = await ps_api_service.create_functional_account_on_platform(
            platform_id=int(platform_id), account_name=username, display_name=label,
            password=password,
            # Says which POV and which dashboard, so an admin who finds it in their tenant
            # knows what it belongs to. No credential, and no customer name — the POV's
            # own name is the only identifier this database is approved to hold.
            description=(f"Created by the VM dashboard for POV {env.name} to onboard its "
                         f"{fam} guests. Removed when that POV is torn down."),
            tenant=api)
    except Exception as exc:  # noqa: BLE001
        logger.warning("POV %s: creating the %s functional account failed", env.id, fam,
                       exc_info=True)
        raise FunctionalAccountError(
            f"could not create a {fam} functional account ({label!r}, account "
            f"{username!r}) on platform {pname!r} in this POV's Password Safe tenant "
            f"({type(exc).__name__}). The API account needs permission to create "
            f"functional accounts; otherwise create one by hand and name it on the "
            f"tenant.") from None

    _record_id(db, env, fam, int(account_id))
    logger.info("POV %s: created %s functional account %s (%r on platform %r)",
                env.id, fam, account_id, label, pname)
    # `created` distinguishes this from the two paths that resolve an existing account, so
    # the caller can say so in the JOB log. Creating a credential in a customer's tenant is
    # not something to leave only in the dashboard's own log: an SE reading the run needs
    # to know what now exists over there, and teardown's claim to remove it.
    return {"id": int(account_id), "platform_id": int(platform_id),
            "platform_name": pname, "account_name": username, "created": True}


async def cleanup(db: Session, env: PovEnvironment, *, api=None) -> str:
    """Delete the functional accounts this dashboard minted for ``env``. Returns a line.

    Never raises: a functional account left in a customer's tenant is untidy, and stopping
    the rest of a teardown over it would leave something worse behind.

    **Only ids recorded on this row.** An account named on the tenant is the customer's
    and is never touched. Call this AFTER the managed systems are off-boarded — Password
    Safe rejects deleting an account a managed system still references, and that rejection
    is the correct outcome, not something to work around.
    """
    minted = [(fam, recorded_id(env, fam)) for fam in FAMILIES]
    minted = [(fam, fid) for fam, fid in minted if fid]
    if not minted:
        return ""

    removed, problems = [], []
    for fam, fid in minted:
        try:
            await ps_api_service.delete_functional_account(fid, api)
        except Exception as exc:  # noqa: BLE001
            logger.warning("POV %s: deleting the %s functional account %s failed",
                           env.id, fam, fid, exc_info=True)
            problems.append(f"{fam} ({fid}): {type(exc).__name__}")
            continue
        # Cleared only on a confirmed delete. Clearing it optimistically is how an account
        # in a customer's tenant becomes invisible from here — the same rule the per-VM
        # terraform states follow.
        _record_id(db, env, fam, 0)
        removed.append(fam)

    if problems:
        return (f"Deleted {len(removed)} functional account(s) this POV created; "
                f"{len(problems)} could not be and are still in the tenant — remove them "
                f"by hand: {'; '.join(problems)}. A still-referenced managed system is "
                f"the usual cause.")
    return f"Deleted the {', '.join(removed)} functional account(s) this POV created."
