"""Structured logging + redaction.

One line of JSON per event to stdout — CloudWatch, Application Insights and Cloud
Logging all capture stdout, so this is the only sink that works unmodified on all
three clouds.

``redact`` is a pure function and the most important thing in this module to keep
correct: every header dict and every body this runtime logs goes through it first.

Stdlib only.
"""
import json
import re
import sys
import time

# Keys whose value is replaced wholesale. Matched case-insensitively against the
# exact key name.
_REDACT_KEYS = frozenset({
    "authorization", "auth", "cookie", "set-cookie",
    "secret", "shared_secret", "fn_shared_secret",
    "password", "passwd", "pwd",
    "token", "api_key", "apikey", "x-api-key", "x-functions-key",
    "client_secret", "private_key", "credential", "credentials",
    "connection_string", "sas", "sas_url", "package_sas_url",
})

# Substrings that make ANY key sensitive, so `db_password`, `admin_password`,
# `azure_client_secret`, `bearer_token` etc. are caught without enumerating them.
# Deliberately does NOT include "key" — that would redact `public_key`, `key_name`
# and other harmless fields, training people to ignore the output.
_REDACT_SUBSTRINGS = ("password", "secret", "token", "credential", "authorization")

_MAX_STR = 512      # longer strings are truncated (a body can be megabytes)
_MAX_ITEMS = 50     # lists are capped
_MAX_DEPTH = 6      # and nesting, so a hostile payload can't blow the stack

# Secrets that arrive INSIDE a string rather than under a key of their own, which
# key-based redaction cannot see. Two shapes matter and both are real:
#
#   * a database driver quotes the offending SQL back in its error message, and the
#     offending SQL for a JIT grant is
#     ``CREATE USER … IDENTIFIED BY '<the password just minted>'``
#   * a workload raises ``RuntimeError("… password=hunter2")``
#
# The hard part is NOT matching more; it is matching less. MySQL's own
# "Your password does not satisfy the current policy requirements" (1819) is a
# ``password``-bearing message with no secret in it, and mangling it would destroy the
# diagnostic this scrub exists to make loggable. So an UNQUOTED value is only ever
# taken after an explicit ``=``/``:`` separator, or after ``IDENTIFIED BY`` — where
# the next token cannot be anything but the literal.
#
# Each pattern keeps the clause in group 1 and the secret in group 2, so the
# substitution rebuilds the clause around ``***`` and the message stays readable.
_SECRET_WORDS = "password|passwd|pwd|secret|token|credential"
# A quoted SQL literal, either quote style.
_QUOTED = "|".join([r"'[^']*'", r'"[^"]*"'])

_SCRUB_PATTERNS = (
    # MySQL: IDENTIFIED BY 'pw' / IDENTIFIED WITH plugin BY pw
    re.compile(r"(identified\s+(?:with\s+\S+\s+)?by\s+)(" + _QUOTED + r"|\S+)",
               re.IGNORECASE),
    # Postgres: … WITH LOGIN PASSWORD 'pw'. QUOTED ONLY — see the note above.
    re.compile(r"((?:" + _SECRET_WORDS + r")\s+)(" + _QUOTED + r")", re.IGNORECASE),
    # SQL Server: WITH PASSWORD = 'pw'; and any password=… / token: … in free text.
    re.compile(r"((?:" + _SECRET_WORDS + r")\s*[=:]\s*)(" + _QUOTED + r"|[^\s,;)]+)",
               re.IGNORECASE),
)


def scrub_text(text):
    """A log-safe copy of a free-text string: credentials embedded in it masked.

    Pure, never raises, and applied to every string :func:`redact` walks — so a
    secret cannot reach the log by riding inside a message instead of under a
    sensitive key.
    """
    if not isinstance(text, str):
        return text
    try:
        for pattern in _SCRUB_PATTERNS:
            text = pattern.sub(lambda m: m.group(1) + "'***'", text)
    except Exception:
        return "<scrub failed>"
    return text


def is_sensitive(key) -> bool:
    """True if a value under ``key`` must never be logged."""
    k = str(key).lower()
    return k in _REDACT_KEYS or any(s in k for s in _REDACT_SUBSTRINGS)


def redact(obj, _depth: int = 0):
    """A log-safe copy of ``obj``: sensitive values masked, long strings and deep
    or wide structures truncated. Pure — never mutates the input, never raises."""
    if _depth > _MAX_DEPTH:
        return "<truncated: max depth>"
    if isinstance(obj, dict):
        out = {}
        for key, val in obj.items():
            out[key] = "***" if is_sensitive(key) else redact(val, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        items = [redact(v, _depth + 1) for v in list(obj)[:_MAX_ITEMS]]
        extra = len(obj) - _MAX_ITEMS
        if extra > 0:
            items.append(f"<{extra} more>")
        return items
    if isinstance(obj, (bytes, bytearray)):
        return f"<{len(obj)} bytes>"
    if isinstance(obj, str):
        # Scrub BEFORE truncating, so a clause split by the cut cannot leak its tail.
        obj = scrub_text(obj)
        if len(obj) > _MAX_STR:
            return obj[:_MAX_STR] + f"…<truncated {len(obj) - _MAX_STR} chars>"
    return obj


def emit(level: str, msg: str, **fields) -> None:
    """Write one redacted JSON line to stdout. Never raises — a logging failure
    must not fail the invocation."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "msg": msg,
    }
    try:
        record.update(redact(fields))
        line = json.dumps(record, default=str)
    except Exception:
        line = json.dumps({"ts": record["ts"], "level": "error",
                           "msg": "log serialization failed"})
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass
