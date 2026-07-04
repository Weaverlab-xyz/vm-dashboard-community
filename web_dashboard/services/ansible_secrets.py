"""Resolve Secrets-Management sources to values for injection into an Ansible run.

A *source* is either a config-secret **registry key** (resolved via
``config_service.get`` — which transparently returns a DB value or resolves an
external-vault reference) or a **raw vault reference** string like
``bt_safe://Dashboard/db_pw`` (resolved via ``resolve_reference``).

Kept tiny and dependency-injected so it unit-tests without ``config_service``.
The resolved values are secret — the caller injects them via a 0600 tmpfile and
must never persist or log them.
"""
from typing import Callable, Optional


def resolve_secret_vars(secret_vars: Optional[dict], *,
                        get: Callable[[str], str],
                        resolve_reference: Callable[[str], str],
                        is_reference: Callable[[str], bool]) -> dict:
    """Map ``{ansible_var: source}`` to ``{ansible_var: resolved_value}``.

    Blank var names / sources and sources that resolve to empty are dropped, so a
    misconfigured row never injects an empty override.
    """
    out: dict = {}
    for var, source in (secret_vars or {}).items():
        var = (var or "").strip()
        source = (source or "").strip()
        if not var or not source:
            continue
        value = resolve_reference(source) if is_reference(source) else get(source)
        if value:
            out[var] = value
    return out
