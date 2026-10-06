"""Okta through its management API (``/api/v1``).

Two ways to authenticate:

* ``ssws`` — an API token. It acts with the role of the admin who made it, so give it
  to a dedicated read-only admin (plus Group Membership Admin for writes).
* ``private_key_jwt`` — an API Services app. Okta only accepts a signed client
  assertion for its own management scopes, so the row's secret is the app's PEM private
  key and ``options.key_id`` names the key. Scopes asked for: ``okta.users.read`` and
  ``okta.groups.read``, plus ``okta.groups.manage`` when writes are enabled.

The endpoint must be the org's own ``*.okta.com`` / ``*.oktapreview.com`` /
``*.okta-emea.com`` / ``*.okta-gov.com`` URL: the management API is served there even
when the org has a custom sign-in domain, and pinning the suffix keeps an operator-typed
endpoint from aiming the dashboard at an internal host.
"""
import re
import time
import uuid
from urllib.parse import urlsplit

from .base import (IdPConnection, IdPError, checked_next, group_item, page_size, request,
                   user_item)

LABEL = "Okta"
_ALLOWED_SUFFIXES = (".okta.com", ".oktapreview.com", ".okta-emea.com", ".okta-gov.com")
_ID_RE = re.compile(r"^[0-9A-Za-z]{1,64}$")
_READ_SCOPES = ("okta.users.read", "okta.groups.read")
_WRITE_SCOPE = "okta.groups.manage"


def normalize_endpoint(endpoint: str) -> str:
    """``https://<org>.okta.com`` form, or "" when it is not an Okta org URL."""
    raw = (endpoint or "").strip().rstrip("/")
    if raw and "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.port not in (None, 443) or parts.username \
            or parts.path not in ("", "/") or parts.query:
        return ""
    if not host.endswith(_ALLOWED_SUFFIXES) or not re.match(r"^[a-z0-9.-]+$", host):
        return ""
    return f"https://{host}"


def _api(conn: IdPConnection) -> str:
    base = normalize_endpoint(conn.endpoint)
    if not base:
        raise IdPError(f"{LABEL}: {conn.endpoint!r} is not an Okta org URL")
    return f"{base}/api/v1"


def _id(value: str, what: str) -> str:
    value = (value or "").strip()
    if not _ID_RE.match(value):
        raise IdPError(f"{LABEL}: that is not an Okta {what} id")
    return value


def _client_assertion(conn: IdPConnection, token_url: str) -> str:
    from jose import jwt
    now = int(time.time())
    headers = {"kid": conn.options.get("key_id")} if conn.options.get("key_id") else None
    try:
        return jwt.encode({"iss": conn.client_id, "sub": conn.client_id, "aud": token_url,
                           "iat": now, "exp": now + 300, "jti": str(uuid.uuid4())},
                          conn.secret, algorithm="RS256", headers=headers)
    except Exception as exc:  # noqa: BLE001 — never echo a key parse error
        raise IdPError(f"{LABEL}: the private key could not sign a client assertion — "
                       f"it must be the app's PEM RSA private key") from exc


def scopes(conn: IdPConnection) -> str:
    want = list(_READ_SCOPES)
    if conn.writes_enabled:
        want.append(_WRITE_SCOPE)
    return " ".join(want)


async def auth_header(conn: IdPConnection) -> tuple:
    if conn.auth_mode == "ssws":
        # No expiry of its own; credentials.py bounds how long the value is held.
        return f"SSWS {conn.secret.strip()}", 0.0
    if conn.auth_mode != "private_key_jwt":
        raise IdPError(f"{LABEL}: unsupported auth mode {conn.auth_mode!r}")
    token_url = f"{normalize_endpoint(conn.endpoint)}/oauth2/v1/token"
    resp = await request(
        f"{LABEL} sign-in", "POST", token_url,
        data={"grant_type": "client_credentials", "scope": scopes(conn),
              "client_assertion_type":
                  "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
              "client_assertion": _client_assertion(conn, token_url)})
    body = resp.json()
    if (body.get("token_type") or "").lower() != "bearer":
        raise IdPError(f"{LABEL}: the app requires DPoP-bound tokens, which this dashboard "
                       f"does not send — turn off 'Require DPoP' on the API Services app")
    ttl = float(body.get("expires_in") or 3600)
    return f"Bearer {body['access_token']}", time.monotonic() + ttl - 60


def _h(header: str) -> dict:
    return {"Authorization": header, "Accept": "application/json"}


def _quote_term(q: str) -> str:
    return re.sub(r'["\\]', "", (q or "").strip())[:100]


def _user(u: dict) -> dict:
    p = u.get("profile") or {}
    name = " ".join(x for x in (p.get("firstName"), p.get("lastName")) if x)
    return user_item(id=u.get("id"), display_name=name or p.get("displayName") or "",
                     login=p.get("login"), email=p.get("email"),
                     enabled=(u.get("status") == "ACTIVE") if u.get("status") else None)


def group_editable(g: dict) -> str:
    kind = g.get("type") or ""
    if kind == "OKTA_GROUP":
        return ""
    if kind == "APP_GROUP":
        return "imported from an app or directory: change it at its source"
    if kind == "BUILT_IN":
        return "a built-in group (Everyone): Okta manages its members"
    return f"group type {kind or 'unknown'} is not editable here"


def _group(g: dict) -> dict:
    p = g.get("profile") or {}
    return group_item(id=g.get("id"), name=p.get("name"), kind=(g.get("type") or "").lower(),
                      description=p.get("description"), editable_reason=group_editable(g))


async def _page(conn, header, path, params, cursor, label):
    api = _api(conn)
    if cursor:
        resp = await request(f"{LABEL} {label}", "GET", checked_next(cursor, api),
                             headers=_h(header))
    else:
        resp = await request(f"{LABEL} {label}", "GET", f"{api}{path}",
                             headers=_h(header), params=params)
    nxt = (resp.links.get("next") or {}).get("url") or ""
    rows = resp.json()
    return (rows if isinstance(rows, list) else []), nxt


async def test(conn: IdPConnection, header: str) -> dict:
    api = _api(conn)
    await request(f"{LABEL} test", "GET", f"{api}/users", headers=_h(header),
                  params={"limit": "1"})
    await request(f"{LABEL} test", "GET", f"{api}/groups", headers=_h(header),
                  params={"limit": "1"})
    note = ("" if conn.auth_mode == "private_key_jwt" else
            " An API token acts with its admin's role; writes need Group Membership Admin "
            "or higher.")
    return {"ok": True, "missing": [], "writes_missing": [],
            "detail": "Okta answered." + note}


async def list_users(conn, header, q="", cursor=""):
    params = {"limit": str(page_size(conn))}
    term = _quote_term(q)
    if term:
        params["search"] = " or ".join(f'profile.{f} sw "{term}"'
                                       for f in ("login", "email", "firstName", "lastName"))
    rows, nxt = await _page(conn, header, "/users", params, cursor, "users")
    return {"items": [_user(u) for u in rows], "next": nxt}


async def list_groups(conn, header, q="", cursor=""):
    params = {"limit": str(page_size(conn))}
    term = _quote_term(q)
    if term:
        params["search"] = f'profile.name sw "{term}"'
    rows, nxt = await _page(conn, header, "/groups", params, cursor, "groups")
    return {"items": [_group(g) for g in rows], "next": nxt}


async def get_group(conn, header, gid):
    gid = _id(gid, "group")
    resp = await request(f"{LABEL} group", "GET", f"{_api(conn)}/groups/{gid}",
                         headers=_h(header))
    return _group(resp.json())


async def group_members(conn, header, gid, cursor=""):
    gid = _id(gid, "group")
    rows, nxt = await _page(conn, header, f"/groups/{gid}/users",
                            {"limit": str(page_size(conn))}, cursor, "members")
    return {"items": [_user(u) for u in rows], "next": nxt}


async def user_groups(conn, header, uid):
    uid = _id(uid, "user")
    rows, _ = await _page(conn, header, f"/users/{uid}/groups", {}, "", "user groups")
    return {"items": [_group(g) for g in rows], "next": ""}


async def add_member(conn, header, gid, uid) -> dict:
    gid, uid = _id(gid, "group"), _id(uid, "user")
    # PUT is idempotent in Okta: re-adding a member is a 204 too, so "changed" is
    # what was asked for rather than what Okta can confirm.
    await request(f"{LABEL} add member", "PUT", f"{_api(conn)}/groups/{gid}/users/{uid}",
                  headers=_h(header))
    return {"changed": True}


async def remove_member(conn, header, gid, uid) -> dict:
    gid, uid = _id(gid, "group"), _id(uid, "user")
    await request(f"{LABEL} remove member", "DELETE",
                  f"{_api(conn)}/groups/{gid}/users/{uid}", headers=_h(header))
    return {"changed": True}
