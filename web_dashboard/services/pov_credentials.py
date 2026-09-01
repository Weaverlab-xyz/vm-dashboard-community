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

**Refuse on ambiguity only where nothing can resolve it.** ``pick`` returns one pair and
refuses when the entries offer two, because its caller seals that one credential into a run
bundle the *agent* uses over WinRM: it never authenticates, so a position in a list is the
only thing it could go on, and installing a Resource Broker as the wrong account is not a
mistake worth being clever about. ``candidates`` exists for the caller that *can* try — the
SSH runner install in ``pov_template_builder`` — where which entry was meant is a question
one round trip answers.

**Never put the text in an error, a log line or a job message.** It contains the password
by definition. Only the *parsed username* is safe to name, and only after parsing
succeeded.

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


def _none_usable(problems: list[str], vm_label: str, remedy: str) -> CredentialParseError:
    """The refusal both public functions raise when nothing parsed, in one wording: the
    reader's next move is the same whether the caller could have tried two logins or not."""
    detail = f" ({problems[0]})" if problems else ""
    return CredentialParseError(
        f"{vm_label} has no stored credential this dashboard can use{detail}. Add one "
        f"on the VM in the lab platform. {remedy}")


# The default remedy names the POV's own login field, which is where a caller that HAS one
# sends the reader. A template build has no such field — its row carries a broker VM name
# and nothing to log in with — so it passes its own. Advice you cannot follow is worse than
# no advice: it sends an SE looking for a control that does not exist.
DEFAULT_REMEDY = "Leave one on the VM, or set the login on the POV by hand."

# A bound on how many logins a caller will throw at one host. Past a handful the box holds
# notes that happen to parse rather than credentials, and every extra one is a real
# authentication attempt against a real guest.
MAX_CANDIDATES = 4


def pick(entries: list, *, vm_label: str = "the VM",
         remedy: str = DEFAULT_REMEDY) -> tuple[str, str]:
    """The one credential in ``entries``, parsed. Ambiguity is a refusal.

    ``entries`` is what an adapter's ``stored_credentials`` returned. More than one usable
    entry is refused rather than resolved by order: which one an SE meant is not something
    a position in a list can answer, and installing a Resource Broker as the wrong account
    is not a mistake worth being clever about.

    ``remedy`` is the sentence that tells the reader what to DO about either refusal; see
    ``DEFAULT_REMEDY``.
    """
    usable, problems = _usable(entries)
    if not usable:
        raise _none_usable(problems, vm_label, remedy)
    if len(usable) > 1:
        # Names how many, never which — the count is diagnostic, the contents are not.
        raise CredentialParseError(
            f"{vm_label} has {len(usable)} usable stored credentials and there is no way "
            f"to tell which is meant. {remedy}")
    return usable[0]


def candidates(entries: list, *, vm_label: str = "the VM",
               remedy: str = DEFAULT_REMEDY) -> list[tuple[str, str]]:
    """Every usable credential in ``entries``, in the order the platform returned them.

    For a caller that can TRY a login and be told it was wrong. ``pick`` refuses on more
    than one because it cannot — it hands its credential to something else to use, so order
    is the only thing it could go on. An SSH install authenticates in process, so the same
    ambiguity is a question the wire answers in one round trip, and the caller finds out
    which login won instead of guessing.

    Raises :class:`CredentialParseError` only when NONE are usable: the same refusal
    ``pick`` raises, in the same words, because the reader's next move is the same.
    """
    usable, problems = _usable(entries)
    if not usable:
        raise _none_usable(problems, vm_label, remedy)
    # Truncation is silent on purpose. The caller's job is to install a runner, not to audit
    # a credential box, and it has no way to tell a fifth login from a fifth note.
    return usable[:MAX_CANDIDATES]
