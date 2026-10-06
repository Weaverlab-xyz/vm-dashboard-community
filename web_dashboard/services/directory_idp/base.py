"""What every cloud identity provider module shares: the resolved connection, the
normalised result shapes, and one HTTP helper.

A provider module (``entra``, ``okta``, ``pingone``) exposes the same async functions,
each taking an :class:`IdPConnection` first:

* ``auth_header(conn) -> (header_value, expires_at_monotonic)``
* ``test(conn) -> dict`` — ``{"ok": True, "detail": str, "missing": [...]}``
* ``list_users(conn, q, cursor)`` / ``list_groups(conn, q, cursor)`` /
  ``group_members(conn, gid, cursor)`` → ``{"items": [...], "next": str}``
* ``user_groups(conn, uid)`` → ``{"items": [...], "next": ""}``
* ``get_group(conn, gid)`` → one normalised group
* ``add_member(conn, gid, uid)`` / ``remove_member(conn, gid, uid)``

and the pure ``group_editable(raw_group) -> str`` (a refusal, or "").

``next`` is opaque to the browser but NOT trusted on the way back: every module checks
a returned cursor against its own API base with :func:`checked_next` before following
it, so a cursor cannot aim the dashboard's credential at another host.
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200
_TIMEOUT = 30
# A 429 is retried once, waiting what the provider asked for up to this many seconds.
_MAX_RATE_LIMIT_WAIT = 10.0

# Tests set this to an httpx.MockTransport; production leaves it None.
transport = None


class IdPError(Exception):
    """An identity-provider call failed. The message is written for the operator and is
    safe to return: it names the HTTP status and, at most, the provider's own short
    error text — never a credential, a token or a traceback."""


@dataclass(frozen=True)
class IdPConnection:
    """A directory row resolved for one call, credential included.

    Frozen and never serialised: ``secret`` is plaintext (a client secret, an Okta API
    token or a PEM key). Like ``hypervisor_connection_service.Connection`` it must not
    reach a log line, a job's metadata or an API response."""
    directory_id: str
    provider: str
    endpoint: str
    tenant_id: str
    client_id: str
    auth_mode: str
    secret: str = ""
    options: dict = field(default_factory=dict)
    page_size: int = PAGE_SIZE_DEFAULT
    writes_enabled: bool = False

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return (f"IdPConnection(provider={self.provider!r}, "
                f"directory_id={self.directory_id!r}, auth_mode={self.auth_mode!r})")


def user_item(*, id, display_name="", login="", email="", enabled=None) -> dict:
    return {"id": str(id or ""), "display_name": display_name or "", "login": login or "",
            "email": email or "", "enabled": enabled}


def group_item(*, id, name="", kind="", description="", editable_reason="") -> dict:
    return {"id": str(id or ""), "name": name or "", "kind": kind or "",
            "description": description or "", "editable": not editable_reason,
            "editable_reason": editable_reason or ""}


def page_size(conn: IdPConnection) -> int:
    try:
        n = int(conn.page_size or PAGE_SIZE_DEFAULT)
    except (TypeError, ValueError):
        n = PAGE_SIZE_DEFAULT
    return max(1, min(n, PAGE_SIZE_MAX))


def checked_next(cursor: str, base: str) -> str:
    """``cursor`` when it is a URL under ``base`` (same scheme, host and path prefix),
    else raise. The only thing standing between a browser-supplied cursor and the
    dashboard sending its bearer token somewhere else."""
    cursor = (cursor or "").strip()
    if not cursor:
        return ""
    c, b = urlsplit(cursor), urlsplit(base)
    if (c.scheme, c.netloc.lower()) != (b.scheme, b.netloc.lower()) \
            or not c.path.startswith(b.path.rstrip("/") + "/") or c.username or c.password:
        raise IdPError("that page cursor does not belong to this directory — reload the list")
    return cursor


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _provider_message(resp) -> str:
    """The provider's own short error text, if it sent one in a known shape."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, dict):                       # Graph: {"error": {"code","message"}}
        return _short(err.get("message") or err.get("code") or "")
    for key in ("errorSummary", "error_description", "message", "detail"):  # Okta, OAuth, PingOne
        if body.get(key):
            return _short(body[key])
    if isinstance(err, str):
        return _short(err)
    return ""


def _retry_after(resp) -> float:
    for header in ("Retry-After",):
        try:
            return float(resp.headers.get(header) or 0)
        except ValueError:
            pass
    reset = resp.headers.get("X-Rate-Limit-Reset")  # Okta: epoch seconds
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            pass
    return 1.0


async def request(label: str, method: str, url: str, *, headers: Optional[dict] = None,
                  params=None, json=None, data=None, auth=None,
                  ok=(200, 201, 204)):
    """One HTTP call with the shared error mapping. Returns the httpx response."""
    import httpx
    kwargs = {"timeout": _TIMEOUT}
    if transport is not None:
        kwargs["transport"] = transport
    async with httpx.AsyncClient(**kwargs) as client:
        for attempt in (0, 1):
            try:
                resp = await client.request(method, url, headers=headers, params=params,
                                            json=json, data=data, auth=auth)
            except httpx.HTTPError as exc:
                raise IdPError(f"{label}: could not reach the provider "
                               f"({type(exc).__name__})") from exc
            if resp.status_code == 429 and attempt == 0:
                await asyncio.sleep(min(_retry_after(resp), _MAX_RATE_LIMIT_WAIT))
                continue
            break
    if resp.status_code in ok:
        return resp
    msg = _provider_message(resp)
    hint = {401: "the credential was rejected",
            403: "the credential lacks a permission this needs",
            404: "not found",
            429: "the provider is rate-limiting this dashboard, try again shortly"
            }.get(resp.status_code, "the provider refused the call")
    raise IdPError(f"{label}: {hint} (HTTP {resp.status_code})" + (f" — {msg}" if msg else ""))
