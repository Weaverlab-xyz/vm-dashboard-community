"""Structured logging + redaction.

One line of JSON per event to stdout — CloudWatch, Application Insights and Cloud
Logging all capture stdout, so this is the only sink that works unmodified on all
three clouds.

``redact`` is a pure function and the most important thing in this module to keep
correct: every header dict and every body this runtime logs goes through it first.

Stdlib only.
"""
import json
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
    if isinstance(obj, str) and len(obj) > _MAX_STR:
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
