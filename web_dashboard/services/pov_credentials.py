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

**Refuse on ambiguity too.** Two credential entries, or one that yields two plausible
pairs, is not an invitation to pick. The operator-supplied fallback exists for exactly
this, and for a platform whose capability table says ``stored_credentials: False``.

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


# The default remedy names the POV's own login field, which is where a caller that HAS one
# sends the reader. A template build has no such field — its row carries a broker VM name
# and nothing to log in with — so it passes its own. Advice you cannot follow is worse than
# no advice: it sends an SE looking for a control that does not exist.
DEFAULT_REMEDY = "Leave one on the VM, or set the login on the POV by hand."


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
    usable = []
    problems = []
    for entry in entries or []:
        text = (entry or {}).get("text") if isinstance(entry, dict) else None
        try:
            usable.append(parse(text or ""))
        except CredentialParseError as exc:
            problems.append(str(exc))

    if not usable:
        detail = f" ({problems[0]})" if problems else ""
        raise CredentialParseError(
            f"{vm_label} has no stored credential this dashboard can use{detail}. Add one "
            f"on the VM in the lab platform. {remedy}")
    if len(usable) > 1:
        # Names how many, never which — the count is diagnostic, the contents are not.
        raise CredentialParseError(
            f"{vm_label} has {len(usable)} usable stored credentials and there is no way "
            f"to tell which is meant. {remedy}")
    return usable[0]
