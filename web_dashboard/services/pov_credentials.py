"""Turning a lab platform's stored credential into a username and a password.

Slice 5b. The Resource Broker install is an Ansible run over WinRM against a Windows VM
inside a POV, and it needs a login for that VM. The platform already holds one — Skytap's
``stored_credentials`` capability, declared since slice 1 — so nothing new has to be stored
in this database.

**The catch is in the contract's own shape.** ``lab_platforms.WRITE_CONTRACT`` says
``stored_credentials`` returns ``[{text, notes}]``, not ``[{username, password}]``, because
Skytap stores *what somebody typed into a box*. In practice that is
``administrator / Passw0rd``, or ``administrator:Passw0rd``, or a sentence with the pair
somewhere inside it.

So this module parses, and the whole of its design is about what to do when it cannot:

**Refuse rather than guess.** A wrong username does not fail as "wrong username" — it comes
back from WinRM as an authentication failure, which reads as a bad password and sends an SE
to reset one that was fine. A refusal naming the VM and quoting nothing costs one message;
a guess costs an afternoon.

**Several credentials on one host is the NORM, and a rule beats a refusal.** A lab guest
carries the superuser its template promised plus whatever else an SE left in the box, so
refusing on "there are two" refused the common case. What resolves it is not the order the
platform returned them in — that is still a guess, and still forbidden — but the guest's
own operating system: a Windows POV guest has ``administrator`` and a Linux one has
``root``, and that is the account these installs are meant to run as. See
``PREFERRED_LOGINS`` and ``_rank``.

**Local beats domain-qualified.** Between two entries the OS rule cannot separate, the
unqualified one wins. A ``DOMAIN\\user`` login depends on a domain controller being up and
reachable at the moment of the run, which in a lab whose boot order is not guaranteed is a
failure that presents as a bad password. The tiebreak is deliberately *second*: a
domain-qualified ``administrator`` still outranks a local ``svc_backup``, because privilege
is what the install needs and availability is what it prefers.

**Refuse only what the rule cannot separate.** Two entries in the same rank — two local
non-superuser logins, say — are still a refusal, because its caller seals that one
credential into a run bundle the *agent* uses over WinRM: it never authenticates, so
nothing downstream would ever discover the wrong one was chosen. That refusal now NAMES the
usernames it could not choose between, and the caller can settle it for good by setting
``prefer``. ``candidates`` exists for the caller that *can* try — the SSH runner install in
``pov_template_builder`` — which gets the same ranking as an order to try them in.

**Never put the text in an error, a log line or a job message.** It contains the password
by definition. Only the *parsed username* is safe to name, and only after parsing
succeeded — which is what lets an ambiguity refusal list the logins it could not choose
between, and what lets a successful pick log the one it took.

Pure and stdlib-only: no platform calls, no database, no config. The caller fetches, this
interprets.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


class CredentialParseError(Exception):
    """The stored credential could not be read as exactly one username and password.

    Its message is operator-facing and travels into a job's ``error_message``, so it names
    the VM and the remedy — and, deliberately, never the text it failed on.
    """


# The separators an operator actually types between a username and a password. Ordered:
# the first that yields a clean split wins, so `user / pass` is not read as `user` and
# `/ pass` by a greedy colon rule.
#
# A bare space is NOT here. "administrator Passw0rd" is indistinguishable from a sentence,
# and a password containing a space would split in the wrong place — which is precisely the
# quiet wrong answer this module exists to avoid.
_SEPARATORS = (
    r"\s*/\s*",        # administrator / Passw0rd
    r"\s*:\s*",        # administrator:Passw0rd
    r"\s*\|\s*",       # administrator | Passw0rd
)

# Labelled forms, tried before the separators: an operator who wrote the words out has told
# us more than one who typed a slash, so believing them costs nothing and reads more cases.
_LABELLED = re.compile(
    r"user(?:name)?\s*[:=]\s*(?P<user>\S+).*?pass(?:word)?\s*[:=]\s*(?P<password>\S+)",
    re.IGNORECASE | re.DOTALL)

# A username Windows would accept: DOMAIN\user, user@domain, or a plain local name. Used to
# reject a "username" that is obviously a sentence fragment rather than to validate one.
_USERNAME = re.compile(r"^[A-Za-z0-9._\-\\@$]{1,104}$")


def valid_username(name: str) -> bool:
    """Could ``name`` be a login this module would ever parse out of a credential box?

    Exported for the one caller that stores a username rather than reading one — the
    per-VM override on the POV page. It is the same test ``parse`` applies, so a value the
    form accepts is a value the selector can match, and an operator cannot save a string
    that would refuse every run afterwards.
    """
    return bool(_USERNAME.match((name or "").strip()))


def parse(text: str) -> tuple[str, str]:
    """``(username, password)`` from one stored-credential string.

    Raises :class:`CredentialParseError` if it is not exactly one pair. The exception
    carries no part of ``text``.
    """
    raw = (text or "").strip()
    if not raw:
        raise CredentialParseError("the stored credential is empty")

    labelled = _LABELLED.search(raw)
    if labelled:
        user, password = labelled.group("user"), labelled.group("password")
        if _USERNAME.match(user) and password:
            return user, password

    # One line only. A multi-line blob is a note with a credential in it somewhere, and
    # picking a line is guessing.
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) != 1:
        raise CredentialParseError(
            "the stored credential is more than one line, so which line holds the login "
            "is a guess")
    line = lines[0]

    for separator in _SEPARATORS:
        parts = re.split(separator, line, maxsplit=1)
        if len(parts) != 2:
            continue
        user, password = parts[0].strip(), parts[1].strip()
        if not user or not password:
            continue
        if not _USERNAME.match(user):
            # The left-hand side is a sentence, not a login. Keep looking rather than
            # returning it — a later separator may split the same line correctly.
            continue
        return user, password

    raise CredentialParseError(
        "the stored credential is not in a form this dashboard can read as a username and "
        "a password. Expected something like 'administrator / Passw0rd'")


def _usable(entries: list) -> tuple[list[tuple[str, str]], list[str]]:
    """Every entry that parses, and the reasons the rest did not.

    An entry that will not parse is not a failure by itself: a credential box often holds a
    note beside the login, and refusing because the note is not a login would refuse the
    common case. It becomes a failure only when nothing else parsed either — which is why
    the reasons come back here rather than being raised.
    """
    usable: list[tuple[str, str]] = []
    problems: list[str] = []
    for entry in entries or []:
        text = (entry or {}).get("text") if isinstance(entry, dict) else None
        try:
            usable.append(parse(text or ""))
        except CredentialParseError as exc:
            problems.append(str(exc))
    return usable, problems


# ── choosing between several ─────────────────────────────────────────────────
#
# The whole reason this module stopped refusing on "there are two". Written as data rather
# than as a chain of ifs because the interesting question is only ever "what does this
# guest OS call its superuser", and the answer is one word per family.

# The login a POV install is MEANT to run as, per guest OS. Not a preference list an
# operator tunes: it is the account a lab template guarantees exists, which is exactly why
# it can be relied on without asking anybody. A family this does not name (a blank
# `os_family`, which slice 3 forbids guessing at) falls back to `_ANY_PREFERRED` below.
PREFERRED_LOGINS = {
    "windows": ("administrator",),
    "linux": ("root",),
}

# For a guest whose family is unknown. Both superuser names are candidates, so a box
# holding exactly one of them still resolves — and one holding BOTH is refused, which is
# correct: that VM's family is the thing nobody has established, and picking either would
# be deciding it by guess. Blank means unknown, never Linux; see PovEnvironmentVM.
_ANY_PREFERRED = tuple(n for names in PREFERRED_LOGINS.values() for n in names)


def _local_part(user: str) -> tuple[str, bool]:
    """``(bare account name, is it domain-qualified)``.

    ``DOMAIN\\alice`` and ``alice@corp.local`` are qualified; ``alice`` and ``.\\alice``
    are not. ``.\\`` is Windows' own "this machine" prefix, so an SE who typed it has
    said local explicitly and is believed.
    """
    raw = (user or "").strip()
    if "\\" in raw:
        prefix, _, account = raw.rpartition("\\")
        # `.` is "this computer"; a bare leading backslash is the same statement.
        return account, prefix.strip() not in ("", ".")
    if "@" in raw:
        account, _, _domain = raw.partition("@")
        return account, True
    return raw, False


def _rank(user: str, wanted: tuple) -> tuple[int, int]:
    """Sortable precedence for one parsed username. Lower wins.

    Two keys, in this order and not the other:

    1. **Is it the superuser this guest OS promises?** What the install needs is
       privilege, and a Resource Broker installed as a service account fails in ways that
       do not say "wrong account".
    2. **Is it local?** What the install prefers is a login that does not depend on a
       domain controller having booted first.

    Second beats first only within a tie, so ``DOMAIN\\administrator`` still outranks a
    local ``btadmin`` — and a local ``administrator`` outranks them both.
    """
    account, qualified = _local_part(user)
    return (0 if account.lower() in wanted else 1, 1 if qualified else 0)


def _wanted(os_family: str) -> tuple:
    """The superuser name(s) that win for this guest OS."""
    fam = (os_family or "").strip().lower()
    return PREFERRED_LOGINS.get(fam) or _ANY_PREFERRED


def _ambiguous(users: list, vm_label: str, remedy: str) -> CredentialParseError:
    """The refusal for entries the rule could not separate.

    Names the usernames, which the module docstring makes safe: a *parsed* username is not
    a credential, and withholding it was costing the reader the one fact that tells them
    what to do next. The passwords, and the raw text they came from, are still nowhere
    near this string.
    """
    listed = ", ".join(sorted(users))
    return CredentialParseError(
        f"{vm_label} offers {len(users)} stored logins of equal standing ({listed}) and "
        f"nothing distinguishes them: none is this guest's superuser, or several are. "
        f"{remedy}")


def _no_such_login(prefer: str, users: list, vm_label: str) -> CredentialParseError:
    """The refusal when an explicit override names a login the platform does not hold.

    Refused rather than ignored. An override that silently falls back to the rule is how a
    typo becomes a run against the wrong account that nobody ever notices — and the run
    would SUCCEED, which is worse than failing.
    """
    listed = ", ".join(sorted(users)) or "none"
    return CredentialParseError(
        f"{vm_label} is set to log in as {prefer!r}, but the lab platform holds no stored "
        f"credential for that account (it holds: {listed}). Correct the login on this VM's "
        f"row, or clear it to let this dashboard choose by guest OS.")


def _none_usable(problems: list[str], vm_label: str, remedy: str) -> CredentialParseError:
    """The refusal both public functions raise when nothing parsed, in one wording: the
    reader's next move is the same whether the caller could have tried two logins or not."""
    detail = f" ({problems[0]})" if problems else ""
    return CredentialParseError(
        f"{vm_label} has no stored credential this dashboard can use{detail}. Add one "
        f"on the VM in the lab platform. {remedy}")


# The default remedy names the per-VM login field on the POV page, which is the control
# that settles an ambiguity for good. A template build has no such row — it works against a
# scratch environment whose VMs this database has never seen — so it passes its own. Advice
# you cannot follow is worse than no advice: it sends an SE looking for a control that does
# not exist.
DEFAULT_REMEDY = ("Set this VM's login on the POV page to say which account to use, or "
                  "leave one credential on the VM in the lab platform.")

# A bound on how many logins a caller will throw at one host. Past a handful the box holds
# notes that happen to parse rather than credentials, and every extra one is a real
# authentication attempt against a real guest.
MAX_CANDIDATES = 4


def pick(entries: list, *, vm_label: str = "the VM", remedy: str = DEFAULT_REMEDY,
         os_family: str = "", prefer: str = "") -> tuple[str, str]:
    """The credential in ``entries`` this guest's install should use, parsed.

    ``entries`` is what an adapter's ``stored_credentials`` returned. Several is the norm,
    not a fault: a lab guest carries the superuser its template promised plus whatever else
    an SE left in the box. Which one is meant is decided by ``_rank`` — the guest OS's own
    superuser first, an unqualified login second — and never by position in the list, which
    remains a guess this module will not make.

    ``os_family`` is ``"windows"``, ``"linux"``, or blank for a guest whose family nobody
    has established. Blank is not treated as Linux; it widens the superuser set to both
    names, so one of them present still resolves and both present is refused.

    ``prefer`` is an operator's explicit answer, and it OUTRANKS everything above. Naming an
    account the platform does not hold is a refusal rather than a fallback: an override that
    quietly reverts to the rule turns a typo into a successful run against the wrong account.

    Raises :class:`CredentialParseError` when nothing parsed, when ``prefer`` matches
    nothing, or when the rule leaves two entries of equal standing — the last of which now
    names them, so the reader knows what to put in ``prefer``.
    """
    usable, problems = _usable(entries)
    if not usable:
        raise _none_usable(problems, vm_label, remedy)

    wanted_login = (prefer or "").strip()
    if wanted_login:
        target = wanted_login.lower()
        # Matched on the whole username OR its bare account name, so an operator who typed
        # `administrator` still selects a stored `CORPdministrator` and does not have to
        # know how the box was filled in.
        hits = [pair for pair in usable
                if pair[0].lower() == target or _local_part(pair[0])[0].lower() == target]
        if not hits:
            raise _no_such_login(wanted_login, [u for u, _ in usable], vm_label)
        if len(hits) > 1:
            raise _ambiguous([u for u, _ in hits], vm_label, remedy)
        logger.info("%s: using the login set on the VM row (%s)", vm_label, hits[0][0])
        return hits[0]

    wanted = _wanted(os_family)
    best = min(_rank(user, wanted) for user, _ in usable)
    winners = [pair for pair in usable if _rank(pair[0], wanted) == best]
    if len(winners) > 1:
        raise _ambiguous([u for u, _ in winners], vm_label, remedy)

    user = winners[0][0]
    if best[1]:
        # Chosen, but worth a line: a domain-qualified login needs a domain controller up
        # at the moment of the run, and when one is not the failure arrives at WinRM as an
        # authentication error — which reads as a bad password. See the module docstring.
        logger.warning(
            "%s: the only login of its standing is domain-qualified (%s); this run depends "
            "on the domain controller being reachable", vm_label, user)
    logger.info("%s: chose the stored login %s of %d (os_family=%r)",
                vm_label, user, len(usable), os_family or "unknown")
    return winners[0]


def candidates(entries: list, *, vm_label: str = "the VM", remedy: str = DEFAULT_REMEDY,
               os_family: str = "") -> list[tuple[str, str]]:
    """Every usable credential in ``entries``, best first.

    For a caller that can TRY a login and be told it was wrong. ``pick`` returns one because
    it hands its credential to something else to use and never learns the outcome; an SSH
    install authenticates in process, so it can afford to work down the list.

    Ordered by the same rule ``pick`` decides with, rather than by the order the platform
    returned them in. That matters even though this caller can retry: the first attempt is
    the one that usually wins, and a run that reaches its guest as ``root`` on the first try
    logs one line instead of three failed authentications an SE has to read past. The sort
    is stable, so entries the rule cannot separate keep the platform's order — which is
    where "the caller finds out which login won" takes over.

    Raises :class:`CredentialParseError` only when NONE are usable: the same refusal
    ``pick`` raises, in the same words, because the reader's next move is the same.
    """
    usable, problems = _usable(entries)
    if not usable:
        raise _none_usable(problems, vm_label, remedy)
    wanted = _wanted(os_family)
    ordered = sorted(usable, key=lambda pair: _rank(pair[0], wanted))
    # Truncation is silent on purpose. The caller's job is to install a runner, not to audit
    # a credential box, and it has no way to tell a fifth login from a fifth note.
    return ordered[:MAX_CANDIDATES]
